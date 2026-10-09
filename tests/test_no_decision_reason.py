"""A cycle that reached no decision has to say why, and two readers got nothing.

OBSERVED WITHIN THREE MINUTES of putting a five-coin rotation on the operator's
live session on 2026-10-09, in the session's own log:

    [XRP/USDT]  DO_NOT_TRADE: the Grid setup is LONG but the panel reads ...
    [SOL/USDT]  DO_NOT_TRADE: the Grid setup is LONG but the panel reads ...
    [DOGE/USDT] NO_DECISION:

Nothing after the colon. The operator's standing question is "why does it only
trade twice a day?", and a third of the rotation was answering it with a blank.

TWO INDEPENDENT DROPS, ONE CAUSE: `summarise_analysis` did not carry the
graph's `unavailable` list, while `market_state`, `monitoring` and
`reflection_graph` all carry theirs. Both readers were already asking for it:

  * `analysis.subscribe_to_triggers` logs
    `"; ".join(result.get("unavailable") ...) or "no reason recorded"` and so
    printed "no reason recorded" on EVERY no-thesis run
  * `trading_session` fell back to `result["noDecisionReason"]`, a key only
    `api/catalog` ever produces from a stored decision row

The reasons were computed the whole time -- every node that cannot run appends
one -- and had nowhere to go. This is the `TarApprovedEvent` shape again: a
defensive read of a value that never arrives is indistinguishable from a
legitimate absence, and the whole chain looks like it is working.
"""

from __future__ import annotations

import inspect

from backend.graphs import analysis
from backend.services import trading_session as ts
from tests.sourceutil import code_only


def test_the_analysis_summary_carries_the_graph_s_own_unavailable_list():
    """The three other graph summaries carry it; this one is what two readers
    were asking for."""
    state = {"unavailable": ["opportunity (no price)", "strategy scoring (no candidates)"]}
    out = analysis.summarise_analysis(state)
    assert out["unavailable"] == [
        "opportunity (no price)",
        "strategy scoring (no candidates)",
    ]


def test_an_empty_run_still_has_the_key():
    """Present and empty, not absent. A reader testing `.get("unavailable")`
    must be able to tell "nothing went wrong" from "this summary predates the
    field" -- the distinction that made the original drop invisible."""
    assert analysis.summarise_analysis({})["unavailable"] == []


def test_the_summary_copies_rather_than_aliasing_the_state_list():
    """`summarise_analysis` is a reporting function. Handing back the live list
    would let a caller mutate graph state through the summary."""
    reasons = ["a"]
    out = analysis.summarise_analysis({"unavailable": reasons})
    out["unavailable"].append("b")
    assert reasons == ["a"]


# ---------------------------------------------------------------------------
# The session's own line
# ---------------------------------------------------------------------------

def test_a_no_decision_cycle_states_the_reason():
    reason = ts._no_decision_reason({
        "unavailable": [
            "historical success rates are taken from the stored backtest",
            "opportunity (no ATR, so no stop-loss can be computed)",
        ],
    })
    assert "no ATR" in reason
    assert reason.startswith("no decision")


def test_the_nearest_reasons_win_over_the_standing_ones():
    """The LAST entries are closest to where the run stopped. The earlier ones
    are usually standing notes -- an absent feed, a missing track record --
    that are true on every cycle and explain nothing about this one, so a
    reader shown only those would conclude the wrong thing."""
    reason = ts._no_decision_reason({
        "unavailable": ["standing note A", "standing note B", "near C", "near D"],
    })
    assert "near C" in reason and "near D" in reason
    assert "standing note A" not in reason


def test_a_run_with_no_recorded_reason_says_so_rather_than_nothing():
    """INVARIANT 6, and the actual bug: an empty string renders as a bare colon,
    which reads as a display fault rather than as a gap in the graph's own
    account of itself. "No reason was recorded" points at the right place."""
    for empty in ({}, {"unavailable": []}, {"unavailable": [None, ""]}):
        out = ts._no_decision_reason(empty)
        assert out, "a reason line may never be empty"
        assert "no reason" in out.lower()


def test_the_session_no_longer_reads_a_key_nothing_produces():
    """`noDecisionReason` is produced by `api/catalog` from a stored decision
    row and has never been on an analysis result. Pinned so the dead fallback
    cannot come back wearing a defensive `.get`."""
    # COMMENTS AND DOCSTRINGS STRIPPED: the comment above the fix has to keep
    # naming the dead key it is explaining, and a mention is not a read.
    assert "noDecisionReason" not in code_only(inspect.getsource(ts))
    decide = inspect.getsource(ts._decide_once)
    assert "_no_decision_reason(result)" in decide


def test_a_real_decision_rationale_still_wins():
    """The reason line is the FALLBACK. A run that reached a decision must
    report the decision's own rationale, not the list of things it could not
    measure along the way."""
    src = inspect.getsource(ts._decide_once)
    i = src.index('decision.get("rationale")')
    j = src.index("_no_decision_reason(result)")
    assert i < j, "the decision's own rationale must be preferred"
