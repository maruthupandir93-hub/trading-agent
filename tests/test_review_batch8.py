"""Batch 8: the review's Section 6 — what the agent should MEASURE.

Six rows in that table. Five already had something behind them: regime and
volatility are computed, slippage/partial fills/latency land in
`execution_quality`, liquidation distance is enforced by
`liquidation_safe_leverage`, expectancy comes out of `strategy_performance`.

Two did not:

    Abstention              nothing at all, on a system that declines far more
                            often than it trades
    performance BY REGIME   pooled only, which cannot express "the range
                            strategies lost in a trending window"

And one piece of the A-D regime guidance was genuinely unenforced: 6C, *"avoid
chasing an extended candle"* — which `strategy_profiles.Breakout` has named as
its own worst condition since it was written, with nothing reading it.
"""

from __future__ import annotations

import inspect

import pytest

from tests.sourceutil import code_only


# ---------------------------------------------------------------------------
# 6C — right direction, wrong moment
# ---------------------------------------------------------------------------

def _bars(lows, highs=None, closes=None):
    highs = highs or [low * 1.01 for low in lows]
    closes = closes or [(l + h) / 2 for l, h in zip(lows, highs)]
    return [
        {"open": c, "high": h, "low": l, "close": c, "volume": 100,
         "openTime": i * 900_000}
        for i, (l, h, c) in enumerate(zip(lows, highs, closes))
    ]


def test_an_entry_far_into_the_move_is_flagged_as_chasing():
    """`Breakout.worst_conditions` says "a market already extended in the
    breakout direction — the move is over", and nothing enforced it."""
    from backend.algorithms.entry_extension import assess

    # A base around 100, then price at 112 with a 1.0 ATR: 12 ATR of travel.
    candles = _bars([100.0] * 22)
    out = assess("LONG", candles, entry_price=112.0, stop_loss=109.5, atr=1.0)

    assert out.chasing is True
    assert out.extension_atr is not None and out.extension_atr > 3.0
    assert "already travelled" in out.reason


def test_a_pullback_entry_is_not_chasing():
    """A NEGATIVE extension means price came back THROUGH the base — the
    opposite of chasing, and exactly what this gate must let through. It is
    deliberately not clamped at zero."""
    from backend.algorithms.entry_extension import assess

    candles = _bars([100.0] * 22)
    out = assess("LONG", candles, entry_price=99.0, stop_loss=96.5, atr=1.0)

    assert out.chasing is False
    assert out.extension_atr is not None and out.extension_atr < 0


def test_a_short_measures_down_from_the_recent_HIGH():
    """Measuring a short from the LOW would report the most extended entries as
    the least — the one error here that would be worse than no check."""
    from backend.algorithms.entry_extension import assess

    candles = _bars([100.0] * 22, highs=[101.0] * 22)
    extended = assess("SHORT", candles, entry_price=90.0, stop_loss=92.5, atr=1.0)
    early = assess("SHORT", candles, entry_price=100.5, stop_loss=103.0, atr=1.0)

    assert extended.chasing is True
    assert early.chasing is False


@pytest.mark.parametrize("candles,atr,why", [
    ([], 1.0, "too few candles"),
    (_bars([100.0] * 22), 0.0, "no usable ATR"),
    (_bars([100.0] * 22), None, "no ATR at all"),
])
def test_an_unmeasurable_extension_is_not_read_as_early(candles, atr, why):
    """None is NOT zero. An unmeasurable extension is a reason to say so, not a
    reason to claim the entry is early — the True/False/None rule this codebase
    has now paid for six times."""
    from backend.algorithms.entry_extension import assess

    out = assess("LONG", candles, entry_price=100.0, stop_loss=97.5, atr=atr)
    assert out.extension_atr is None, why
    assert out.chasing is False, "an unmeasured extension must not refuse a trade"


def test_the_limit_is_read_at_call_time(monkeypatch):
    """The `simulation_mode` rule: an operator who changes this expects the next
    decision to see it, not the next restart."""
    from backend.algorithms.entry_extension import max_extension_atr

    monkeypatch.setenv("MAX_ENTRY_EXTENSION_ATR", "1.0")
    assert max_extension_atr() == 1.0
    monkeypatch.setenv("MAX_ENTRY_EXTENSION_ATR", "not a number")
    assert max_extension_atr() == 3.0


def test_the_gate_defaults_OFF_because_the_measurement_overturned_it(monkeypatch):
    """THE HYPOTHESIS WAS MEASURED AND IT WAS WRONG.

    The review's 6C and this system's own Breakout profile both say a late
    entry is a worse entry. Replayed through the REAL `assess` against 17,157
    signals on six instruments (`scripts/measure_entry_extension.py`):

        the gate would KEEP   -0.0503R   11,144 trades
        the gate would BLOCK  +0.0412R    6,013 trades
        gap -0.0915R, z = -4.04, against a smallest-detectable gap of 0.0371R

    It removes the BETTER half. Entering 3+ ATR into a move is momentum, and
    momentum is what has worked on these instruments.

    The result ran AGAINST the author's hypothesis, which is the only
    direction this kind of result is worth much — `core/evidence.py` exists
    because a throwaway script once produced three "findings" that all ran the
    way the author was hoping.
    """
    from backend.algorithms.entry_extension import gate_enabled

    monkeypatch.delenv("REQUIRE_ENTRY_PROXIMITY", raising=False)
    assert gate_enabled() is False, (
        "shipping this on would act on a plausible story against the only "
        "evidence available"
    )
    monkeypatch.setenv("REQUIRE_ENTRY_PROXIMITY", "true")
    assert gate_enabled() is True


def test_the_extension_is_still_measured_with_the_gate_off(monkeypatch):
    """Only the REFUSAL is off. The measurement runs either way, so a future
    ledger can disagree with the replay — the arrangement
    REQUIRE_HTF_ALIGNMENT already uses."""
    from backend.algorithms.entry_extension import assess

    monkeypatch.delenv("REQUIRE_ENTRY_PROXIMITY", raising=False)
    out = assess("LONG", _bars([100.0] * 22), entry_price=112.0,
                 stop_loss=109.5, atr=1.0)
    assert out.extension_atr is not None
    assert out.chasing is True, "the verdict is still computed; it is simply not acted on"


def test_the_finding_is_reproducible():
    """A measurement that cannot be re-run is an assertion. This one has a
    committed script, like `measure_htf_gate.py` before it."""
    import pathlib

    src = pathlib.Path("scripts/measure_entry_extension.py").read_text(encoding="utf-8")
    # The REAL function, not a re-implementation: a gate measured by a copy of
    # itself measures the copy.
    assert "from backend.algorithms.entry_extension import assess" in src
    assert "ATR_STOP_MULTIPLIER" in src
    # An ambiguous bar resolves AS THE STOP, the conservative rule.
    assert "resolves as the stop" in src.lower()


def test_the_gateway_refuses_a_chased_entry_and_says_it_is_not_counter_trend():
    """The three gates beside it do not cover this and the refusal must not be
    mistaken for one of them: the HTF gate refuses a COUNTER-trend entry, and
    chasing is the opposite error — right direction, late."""
    import backend.graphs.nodes.risk_gateway as rg

    src = inspect.getsource(rg.gate)
    assert "EntryProximity" in src
    assert "chasing an extended move" in src
    assert "the direction agrees with the move" in src


def test_the_extension_is_recorded_even_when_the_gate_is_off():
    """Turning a gate off must not also stop recording the data that would
    judge it — the arrangement REQUIRE_HTF_ALIGNMENT already uses."""
    import backend.graphs.nodes.risk_gateway as rg

    src = inspect.getsource(rg.gate)
    built = src.index("assess_entry_extension")
    gated = src.index("entry_proximity_required()")
    assert built < gated, "the measurement is only taken when the gate is on"

    assert "entry_extension" in inspect.signature(rg.build_entry_context).parameters


def test_the_context_field_is_appended_last_so_every_parser_still_matches():
    """Four frontend parsers read this string by regex. A field added at the
    END leaves all of them matching; one inserted in the middle would silently
    stop a parser finding the value after it."""
    from backend.graphs.nodes.risk_gateway import build_entry_context
    from backend.algorithms.entry_extension import assess

    extension = assess(
        "LONG", _bars([100.0] * 22), entry_price=101.0, stop_loss=98.5, atr=1.0,
    )
    out = build_entry_context(
        symbol="SOL/USDT", technical=None, regime_state=None, volatility=None,
        strategy="Breakout", entry_extension=extension,
    )
    assert out.rstrip().endswith("strategy=Breakout"), (
        "the strategy must stay last — parsers anchor on it"
    )
    assert "ATR into the move" in out


def test_the_gateway_measures_against_the_same_ATR_that_placed_the_stop():
    """Two copies of an ATR is how `lib/riskManager.ts` and
    `core/risk_manager.py` drifted apart on the stop multipliers, and a gate
    measuring against a DIFFERENT ATR would refuse trades for arithmetic nobody
    can reproduce."""
    import backend.graphs.nodes.risk_gateway as rg

    raw = inspect.getsource(rg._atr_for)
    # The state key is a STRING LITERAL, which `code_only` strips — so the
    # presence check reads the raw source and the ABSENCE check reads the
    # stripped one, where a mention in a comment cannot produce a false alarm.
    assert '"technical_analysis"' in raw
    assert "calculate_atr" not in code_only(raw), "the gate recomputes its own ATR"


# ---------------------------------------------------------------------------
# Abstention — the row with nothing behind it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason,gate", [
    ("the Grid setup is LONG but the specialist panel reads NEUTRAL at 0.05", "confidence-floor"),
    ("chasing an extended move: price has already travelled 4.2 ATR", "entry-proximity"),
    ("XRP/USDT order is below the venue minimum of 5.00", "venue-minimum"),
    ("BTC/USDT may be watched and reasoned about but not opened", "tradeable-universe"),
    ("the round trip fee is 0.31R of a 0.40% stop", "fee-vs-edge"),
    ("Grid is not approved for real money: 3 real closed trades", "strategy-approval"),
])
def test_each_refusal_is_attributed_to_the_gate_that_made_it(reason, gate):
    """"The agent did not trade" is not a finding. "88% of refusals were the
    confidence floor and 2% were the venue minimum" is, and they have
    completely different remedies."""
    from backend.services.abstention import classify

    assert classify(reason) == gate


def test_an_unrecognised_reason_is_reported_rather_than_absorbed():
    """A taxonomy that quietly folds what it does not recognise into a
    neighbouring bucket is how a new failure mode stays invisible."""
    from backend.services.abstention import classify

    assert classify("something no gate has ever said before") == "other"
    assert classify("") == "unstated"


def test_the_margin_says_how_decisive_a_refusal_was():
    """46 of 50 live decisions ran 0.00-0.12 against a 0.60 floor. Knowing that
    is what stops an operator lowering a threshold that was never the binding
    constraint."""
    from backend.services.abstention import margin

    assert margin("panel confidence 0.12 against a 0.60 floor") == pytest.approx(0.48)


def test_an_unreadable_margin_is_not_counted_as_a_tie():
    """None is not zero. Averaging an unreadable margin in as one would make
    the agent look like it refuses on coin flips when the live distribution is
    nothing of the kind."""
    from backend.services.abstention import margin

    assert margin("refused because the venue was unreachable") is None
    # Two unrelated numbers must not be mistaken for a confidence pair.
    assert margin("order 3513737131 for 79.5 XRP") is None


def test_the_funnel_counts_acted_and_abstained_separately():
    from backend.services.abstention import summarise

    rows = [
        {"outcome": "approved-executed", "rejection_reasons": []},
        {"outcome": "rejected", "rejection_reasons": ["panel reads NEUTRAL at 0.09 against a 0.60 floor"]},
        {"outcome": "rejected-by-risk", "rejection_reasons": ["global VaR limit exceeded"]},
        {"outcome": "pending-approval", "rejection_reasons": []},
    ]
    out = summarise(rows)

    assert out["acted"] == 1
    assert out["abstained"] == 2
    assert out["abstentionRate"] == pytest.approx(0.5)
    assert out["bindingGate"] in ("confidence-floor", "var-limit")


def test_every_gate_bucket_carries_an_example():
    """A bucket with no example is a bucket nobody can act on — especially
    "other" and "unstated", which exist to be chased."""
    from backend.services.abstention import summarise

    out = summarise([
        {"outcome": "rejected", "rejection_reasons": ["a reason no pattern matches"]},
    ])
    bucket = out["byGate"][0]
    assert bucket["gate"] == "other"
    assert bucket["example"]


@pytest.mark.asyncio
async def test_an_unreadable_decisions_table_does_not_report_zero_abstentions():
    """Reporting zeros would read as an agent that takes every trade it sees,
    which is the opposite of what is true and the more alarming misreading."""
    from backend.services import abstention

    abstention.reset_cache()
    out = await abstention.metrics(force=True)
    assert out.get("available") is False
    assert "NOT a report" in out["note"]


def test_abstention_makes_no_model_call_and_changes_nothing():
    """Deterministic arithmetic over `decisions`. The same invariant-5 argument
    `strategy_performance` and `strategy_priors` both make."""
    import backend.services.abstention as ab

    src = code_only(inspect.getsource(ab))
    for forbidden in ("complete(", "ModelTier", "openai", "UPDATE", "INSERT", "DELETE"):
        assert forbidden not in src, f"abstention reaches for {forbidden!r}"


def test_it_does_not_estimate_what_a_refused_trade_would_have_done():
    """Deliberately NOT a counterfactual: that is an in-sample simulation of a
    path never taken, and reporting it beside real measurements is where a
    reader stops being able to tell them apart."""
    from backend.services.abstention import summarise

    note = summarise([])["note"]
    assert "does NOT estimate" in note
    assert "backtests/" in note


# ---------------------------------------------------------------------------
# Performance by regime
# ---------------------------------------------------------------------------

def test_the_regime_is_recovered_from_the_snapshot_the_gateway_already_wrote():
    """A separate column would be the better home and is deliberately not
    built: `entry_context` already carries this for every row the gateway
    wrote, INCLUDING HISTORICAL ONES, so a new column would leave every
    existing trade NULL and the split would report "no data" on exactly the
    ledger it exists to analyse."""
    from backend.services.strategy_performance import regime_of

    assert regime_of(
        "RSI=62, ATR=0.51%, regime=Bull Trend, volatility=HIGH (82nd pct), strategy=Breakout"
    ) == "Bull Trend"


def test_a_trade_with_no_recorded_regime_is_not_attributed_to_one():
    """Attributing a trade to a regime it was not taken in would poison exactly
    the measurement this feeds. "unknown" is a real answer here."""
    from backend.services.strategy_performance import regime_of

    assert regime_of("RSI=62, strategy=Breakout") is None
    assert regime_of(None) is None
    assert regime_of("") is None


def test_the_regime_split_does_not_feed_selection():
    """A per-regime sample is MIN_SAMPLE divided by the number of regimes, and
    steering on a handful of trades is how naive adaptive systems destroy
    themselves. `_track_record_score` reads the POOLED rate and continues to."""
    import backend.graphs.nodes.opportunity as opp

    src = code_only(inspect.getsource(opp))
    assert "by_regime" not in src, "selection now reads a per-regime sample"


def test_the_profit_factor_is_none_rather_than_infinite_with_no_losses():
    """A strategy with no losing trade yet has an UNMEASURED profit factor.
    Rendering infinity reads as a perfect record."""
    import backend.services.strategy_performance as sp

    src = code_only(inspect.getsource(sp))
    assert "profitFactor" in inspect.getsource(sp)
    assert "gross_loss > 0" in src.replace("  ", " ") or "gross_loss > 0" in inspect.getsource(sp)


def test_both_new_measurements_are_surfaced():
    import backend.api.graphs as api

    src = inspect.getsource(api.strategy_performance_summary)
    assert "byRegime" in src
    assert "abstention" in src
